import { describe, expect, it } from 'vitest';

import {
  parseCurrentSemanticAssessmentResponse,
  resolveSemanticPolicyViewModel,
  semanticPolicyRenderTelemetry,
  type SemanticAnchorResolution,
} from '../semanticPolicyModel';

const digest = 'a'.repeat(64);
const expected = {
  boardId: 'board-1',
  entityType: 'spec' as const,
  subjectId: 'spec-1',
};

function anchor(
  anchorType: 'whole_artifact' | 'field' | 'structured_child' | 'qa',
  anchorRef: string | null,
) {
  return {
    anchor_type: anchorType,
    anchor_ref: anchorRef,
    excerpt_hash: digest,
  };
}

function pinpoint(
  key: string,
  anchorValue: ReturnType<typeof anchor>,
  overrides: Record<string, unknown> = {},
) {
  return {
    contract_version: 'v2',
    pinpoint_key: key,
    kind: 'evidence',
    title: `Evidence ${key}`,
    detail: `Explanation ${key}`,
    severity: null,
    remediation: null,
    anchor: anchorValue,
    anchor_snapshot: {
      label: `Location ${key}`,
      excerpt: `Excerpt ${key}`,
      source_version: '7',
      availability_at_seal: 'available',
    },
    blocking: false,
    ...overrides,
  };
}

function metric(
  code: string,
  pinpoints: unknown[],
  overrides: Record<string, unknown> = {},
) {
  return {
    metric_result_id: `result-${code}`,
    metric_result_digest: digest,
    metric_id: `metric-${code}`,
    metric_code: code,
    score: 90,
    direction: 'minimum',
    default_threshold: 80,
    effective_threshold: 80,
    threshold_source: 'default',
    outcome: 'pass',
    blocking: false,
    pinpoints,
    ...overrides,
  };
}

function response(metrics: unknown[], validationEdition: number | null = null) {
  return {
    contract_version: 'v2',
    assessment: {
      receipt_id: 'receipt-v2',
      receipt_digest: digest,
      currentness: 'current',
      board_id: 'board-1',
      subject_type: 'spec',
      subject_id: 'spec-1',
      subject_version: 7,
      validation_edition: validationEdition,
      binding_id: 'binding-1',
      guideline_id: 'guideline-1',
      guideline_revision_id: 'revision-1',
      confidence: 91,
      recorded_at: '2026-08-08T12:00:00Z',
      metrics,
    },
  };
}

function legacyResponse(currentness: 'current' | 'stale' = 'current') {
  return {
    contract_version: 'v1',
    assessment: {
      projection: 'detail',
      receipt_id: 'receipt-v1',
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
      currentness,
      currentness_reasons: currentness === 'stale'
        ? ['subject_version_changed']
        : [],
      confidence: 90,
      minimum_confidence: 80,
      metric_count: 1,
      failed_metric_count: 0,
      recorded_at: '2026-08-08T12:00:00Z',
      binding_revision: 2,
      assessor_agent_id: 'agent-1',
      assessor_model_id: null,
      assessor_independent: true,
      confidence_admissible: true,
      metric_results: [{
        metric_result_id: 'result-v1',
        metric_id: 'metric-v1',
        metric_code: 'Legacy.Clarity',
        score: 90,
        direction: 'minimum',
        default_threshold: 80,
        effective_threshold: 80,
        threshold_source: 'default',
        outcome: 'pass',
        rationale: 'The legacy rationale remains the explanation.',
        evidence_refs: [{
          source_type: 'spec',
          source_id: 'spec-1',
          source_version: 7,
          content_hash: digest,
        }],
        pinpoints: [{
          anchor_type: 'field',
          anchor_ref: 'technical_requirements',
          excerpt_hash: digest,
          input_digest: digest,
        }],
      }],
    },
  };
}

describe('semanticPolicyModel v2 dual-read resolver', () => {
  it('discriminates exact v1 and v2 field sets without fallback', () => {
    const v2 = response([
      metric('Architecture', [
        pinpoint('whole', anchor('whole_artifact', null)),
      ]),
    ]);

    expect(parseCurrentSemanticAssessmentResponse(v2, expected))
      .toMatchObject({ contract_version: 'v2' });
    expect(parseCurrentSemanticAssessmentResponse(legacyResponse(), expected))
      .toMatchObject({ contract_version: 'v1' });

    expect(() => parseCurrentSemanticAssessmentResponse(
      { ...v2, unexpected: true },
      expected,
    )).toThrow(/unknown or missing field/u);
    expect(() => parseCurrentSemanticAssessmentResponse(
      { ...v2, contract_version: 'v3' },
      expected,
    )).toThrow(/unsupported/u);
    const nestedUnknown = response([
      metric('Architecture', [{
        ...pinpoint('whole', anchor('whole_artifact', null)),
        unexpected: true,
      }]),
    ]);
    expect(() => parseCurrentSemanticAssessmentResponse(
      nestedUnknown,
      expected,
    )).toThrow(/unknown or missing field/u);
  });

  it('rejects null or mismatched v2 current evidence for a lifecycle edition', () => {
    const metrics = [metric('Architecture', [
      pinpoint('whole', anchor('whole_artifact', null)),
    ])];
    const lifecycleExpectation = { ...expected, validationEdition: 2 };

    expect(() => parseCurrentSemanticAssessmentResponse(
      response(metrics, null),
      lifecycleExpectation,
    )).toThrow('active validation edition');
    expect(() => parseCurrentSemanticAssessmentResponse(
      response(metrics, 1),
      lifecycleExpectation,
    )).toThrow('active validation edition');
    expect(parseCurrentSemanticAssessmentResponse(
      response(metrics, 2),
      lifecycleExpectation,
    )).toMatchObject({
      contract_version: 'v2',
      assessment: { validation_edition: 2 },
    });
  });

  it('resolves all anchor types with sealed explanation and live navigation only', () => {
    const parsed = parseCurrentSemanticAssessmentResponse(response([
      metric('Architecture', [
        pinpoint('whole', anchor('whole_artifact', null)),
        pinpoint('field', anchor('field', 'description')),
        pinpoint('child', anchor('structured_child', 'child-secret-id')),
        pinpoint('qa', anchor('qa', 'qa-7')),
      ]),
    ]), expected);
    const resolveAnchor = (value: { anchor_ref: string | null }): SemanticAnchorResolution => {
      if (value.anchor_ref === 'description') return { state: 'removed' };
      if (value.anchor_ref === 'child-secret-id') return { state: 'inaccessible' };
      return {
        state: 'available',
        navigationTarget: value.anchor_ref ? `/focus/${value.anchor_ref}` : '/focus/root',
      };
    };

    const model = resolveSemanticPolicyViewModel(parsed, {
      resolveAnchor,
      canViewTechnicalDetails: true,
    });
    const [whole, field, child, qa] = model.metrics[0].pinpoints;

    expect(whole).toMatchObject({
      state: 'available',
      locationLabel: 'Location whole',
      excerpt: 'Excerpt whole',
      navigationTarget: '/focus/root',
    });
    expect(field).toMatchObject({
      state: 'removed',
      locationLabel: 'Location field',
      excerpt: 'Excerpt field',
      navigationTarget: null,
      unavailableMessage: 'Referenced element is no longer available.',
    });
    expect(child).toMatchObject({
      state: 'inaccessible',
      locationLabel: 'Restricted assessment location',
      excerpt: null,
      navigationTarget: null,
      unavailableMessage: 'Location unavailable with your current access.',
    });
    expect(child.technicalDetails).toEqual({
      anchorType: 'structured_child',
      sourceVersion: '7',
    });
    expect(JSON.stringify(child)).not.toContain('child-secret-id');
    expect(JSON.stringify(child)).not.toContain(digest);
    expect(qa.navigationTarget).toBe('/focus/qa-7');
    expect(model.uiStates).toEqual([
      'positive_evidence',
      'removed',
      'inaccessible',
    ]);
  });

  it('fails closed when no live authorization resolver is provided', () => {
    const parsed = parseCurrentSemanticAssessmentResponse(response([
      metric('Architecture', [
        pinpoint('field', anchor('field', 'secret-field')),
      ]),
    ]), expected);

    const [view] = resolveSemanticPolicyViewModel(parsed).metrics[0].pinpoints;

    expect(view.state).toBe('inaccessible');
    expect(view.navigationTarget).toBeNull();
    expect(view.technicalDetails).toBeNull();
    expect(JSON.stringify(view)).not.toContain('secret-field');
    expect(JSON.stringify(view)).not.toContain(digest);
  });

  it('preserves legacy rationale without inventing severity or remediation', () => {
    const parsed = parseCurrentSemanticAssessmentResponse(
      legacyResponse(),
      expected,
    );
    const model = resolveSemanticPolicyViewModel(parsed, {
      resolveAnchor: () => ({
        state: 'available',
        navigationTarget: '/focus/technical-requirements',
      }),
      canViewTechnicalDetails: true,
    });
    const [view] = model.metrics[0].pinpoints;

    expect(model.uiStates).toEqual(['legacy']);
    expect(view).toMatchObject({
      state: 'legacy',
      kind: 'legacy',
      detail: 'The legacy rationale remains the explanation.',
      severity: null,
      remediation: null,
      locationLabel: 'Technical Requirements',
    });
    expect(view.technicalDetails).toMatchObject({
      anchorReference: 'technical_requirements',
      excerptHash: digest,
      inputDigest: digest,
    });
  });

  it('preserves fail, warning, waived, stale and unavailable UI states', () => {
    const warning = pinpoint(
      'warning',
      anchor('field', 'description'),
      { kind: 'issue', severity: 'low' },
    );
    const blocking = pinpoint(
      'blocking',
      anchor('field', 'architecture'),
      { kind: 'issue', severity: 'high', blocking: true },
    );
    const parsed = parseCurrentSemanticAssessmentResponse(response([
      metric('Evidence', [pinpoint('evidence', anchor('whole_artifact', null))]),
      metric('Warning', [warning]),
      metric('Blocked', [blocking], {
        score: 50,
        outcome: 'fail',
        blocking: true,
      }),
    ]), expected);

    const model = resolveSemanticPolicyViewModel(parsed, {
      resolveAnchor: () => ({ state: 'removed' }),
      waivedMetricCodes: new Set(['Blocked']),
    });

    expect(model.metrics.map((item) => item.uiState)).toEqual([
      'positive_evidence',
      'non_blocking_warning',
      'waived_fail_finding',
    ]);
    expect(model.uiStates).toContain('removed');
    const stale = resolveSemanticPolicyViewModel(
      parseCurrentSemanticAssessmentResponse(legacyResponse('stale'), expected),
    );
    expect(stale.uiStates).toContain('stale');
  });

  it('emits only closed render labels and never assessment payload', () => {
    expect(semanticPolicyRenderTelemetry('fail', 'v2')).toEqual({
      metric: 'pulse_policy_compliance_render_total',
      labels: { contract_version: 'v2', outcome: 'current' },
    });
    expect(semanticPolicyRenderTelemetry('waived_fail_finding', 'v2')
      .labels.outcome).toBe('waived');
    expect(semanticPolicyRenderTelemetry('stale', 'v1').labels.outcome)
      .toBe('stale');
    expect(semanticPolicyRenderTelemetry('legacy', 'v1').labels.outcome)
      .toBe('legacy');
    expect(semanticPolicyRenderTelemetry('removed', 'v2').labels.outcome)
      .toBe('unavailable');
    expect(semanticPolicyRenderTelemetry(
      'recoverable_transport_error',
      'none',
    ).labels.outcome).toBe('system_error');
    expect(Object.keys(semanticPolicyRenderTelemetry('loading', 'none').labels))
      .toEqual(['contract_version', 'outcome']);
  });
});
